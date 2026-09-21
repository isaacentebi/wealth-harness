"""The canonical model: the live-session facts, legacy shapes, brief, profile, insights, threads, prompt."""
from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timezone
from unittest import mock

import pytest

from tests.fixtures.ingest import statements as fixtures
from tests.test_cashflow import ledger as cashflow_ledger
from wealth import agent, situation
from wealth.profile import profile_view
from wealth.service import WealthService, upload_dir
from wealth import store as store_module
from wealth.store import ValidationError

TODAY = datetime.now(timezone.utc).date()


def _source(kind: str = "user") -> dict:
    return {"kind": kind, "ref": "conversation", "observed_on": TODAY.isoformat()}


# Exactly what the agent saved in the 2026-09-21 live session, plus the spending it failed to save.
LIVE = {
    "client.profile": {"residence": {"city": "Ciudad de México", "country": "México"}},
    "income.schedule": {"items": [{"amount": 85000, "currency": "MXN", "frequency": "monthly",
                                   "id": "net_monthly_income", "net": True, "approximate": True}]},
    "plan.resources": {"cash": [{"amount": 150000, "institution": "Nu", "currency": "MXN"}],
                       "debts": [{"annual_rate": 0.13, "balance": 60000, "type": "auto", "currency": "MXN"}],
                       "investments": [{"amount": 200000, "institution": "GBM", "currency": "MXN", "approximate": True}]},
    "goals": [{"id": "sp500_monthly_investing", "amount": 10000, "frequency": "monthly",
               "description": "Invertir aproximadamente MXN 10,000 al mes en el S&P 500"}],
    "spending.monthly": {"total": 45000, "currency": "MXN", "approximate": True},
}
CANONICAL = {
    "client.profile": {"residence": {"city": "Ciudad de México", "country": "MX"}},
    "income.salary": {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True, "approximate": True},
    "cash.nu": {"amount": 150000, "currency": "MXN", "institution": "Nu"},
    "liability.car": {"kind": "auto", "balance": 60000, "currency": "MXN", "annual_rate": 0.13},
    "investment.gbm": {"amount": 200000, "currency": "MXN", "institution": "GBM", "approximate": True},
    "goals": [{"id": "sp500", "name": "Invertir en el S&P 500", "action": "invest", "object": "el S&P 500",
               "monthly_contribution": 10000, "currency": "MXN"}],
    "spending.monthly": {"total": 45000, "currency": "MXN", "approximate": True},
}


def _client(tmp_path, facts: dict, client_id: str = "ana") -> WealthService:
    """A client whose facts were written as given. Legacy shapes (LIVE) predate the schema, as in an
    existing database, so they are written with validation off."""
    service = WealthService(tmp_path / "wealth.sqlite3")
    service.create(client_id, "Ana")
    if facts:
        with mock.patch.object(store_module, "validate_canonical", lambda key, value: []) if facts is LIVE \
                else contextlib.nullcontext():
            service.remember(client_id, [{"key": k, "value": v, "source": _source(), "confidence": "reported"}
                                         for k, v in facts.items()], 0)
    return service


def test_live_session_facts_build_the_whole_picture(tmp_path):
    sit = _client(tmp_path, LIVE).situation("ana")
    json.dumps(sit)
    assert sit["currency"] == "MXN"
    assert sit["net_worth"]["total"] == 290000  # 150,000 cash + 200,000 GBM − 60,000 car
    assert sit["net_worth"]["liquid"] == 350000 and sit["net_worth"]["complete"]
    flow = sit["cash_flow"]
    # The car payment is unknown, so the surplus is unknown (never income − spending as if the car were free);
    # the known part is kept, named as "before the car payment".
    assert (flow["income"], flow["spending"], flow["surplus"]) == (85000, 45000, None)
    assert flow["surplus_before_unknown_debts"] == 40000 and flow["missing"] == ["liability.debt0.payment"]
    assert flow["debt_payments_unknown"] == ["debt0"] and not flow["complete"]
    assert sit["commitments"]["total"] == 10000 and sit["commitments"]["unallocated"] is None
    assert sit["reserve"]["months"] == 3.3 and sit["reserve"]["basis"] == "all undesignated cash"
    car = sit["liabilities"][0]
    assert car["kind"] == "auto" and car["annual_rate"] == 0.13
    assert car["missing"] == ["payment or remaining_term_months"] and car["payoff"] == {"status": "unknown"}
    goal = sit["goals"][0]
    assert goal["name"] == "Invertir aproximadamente MXN 10,000 al mes en el S&P 500"
    assert goal["monthly_contribution"] == 10000 and goal["currency_assumed"]
    assert sit["profile"]["residence"]["country"] == "MX" and sit["profile"]["tax_residence"] is None
    assert {u["code"] for u in sit["unknowns"]} >= {"liability_payment", "tax_residence", "reserve_target"}
    assert set(sit["legacy"]) == {"goals", "income.schedule", "plan.resources"}


def test_legacy_shapes_read_the_same_as_canonical_keys(tmp_path):
    legacy = _client(tmp_path / "a", LIVE).situation("ana")
    canonical = _client(tmp_path / "b", CANONICAL).situation("ana")
    for path in (("net_worth", "total"), ("cash_flow", "surplus"), ("reserve", "months"), ("commitments", "total")):
        assert legacy[path[0]][path[1]] == canonical[path[0]][path[1]], path
    assert canonical["legacy"] == []
    assert canonical["liabilities"][0]["id"] == "car" and canonical["goals"][0]["name"] == "Invertir en el S&P 500"
    # The profile form's own amounts are read too.
    form = _client(tmp_path / "c", {"client.profile": {
        "monthly_income": {"amount": 85000, "currency": "MXN"}, "monthly_spending": {"amount": 45000, "currency": "MXN"},
        "savings": {"amount": 150000, "currency": "MXN"}}}).situation("ana")
    assert form["cash_flow"]["surplus"] == 40000 and form["net_worth"]["total"] == 150000


def test_payoff_date_when_payment_or_term_is_known(tmp_path):
    facts = {**CANONICAL, "liability.car": {**CANONICAL["liability.car"], "payment": 5000, "payment_frequency": "monthly"}}
    sit = situation.build({"client": {"revision": 1}, "facts": [
        {"key": k, "value": v, "id": k, "confidence": "reported", "source": _source(), "expires_on": None}
        for k, v in facts.items()]}, None, date(2026, 9, 21))
    car = sit["liabilities"][0]
    assert car["missing"] == [] and car["payoff"]["status"] == "ready"
    assert car["payoff"]["months"] == 13 and car["payoff"]["date"] == "2027-10"
    assert sit["cash_flow"]["surplus"] == 35000 and sit["cash_flow"]["complete"]
    by_term = situation.build({"client": {"revision": 1}, "facts": [
        {"key": "liability.car", "value": {**CANONICAL["liability.car"], "remaining_term_months": 24},
         "id": "x", "confidence": "reported", "source": _source(), "expires_on": None}]}, None, date(2026, 9, 21))
    assert by_term["liabilities"][0]["payment_basis"] == "from remaining term"
    assert by_term["liabilities"][0]["payoff"]["date"] == "2028-09"


def test_brief_is_stable_short_and_numeric(tmp_path):
    sit = _client(tmp_path, LIVE).situation("ana")
    for language in ("es", "en"):
        text = situation.brief(sit, language)
        assert text == situation.brief(sit, language)
        assert len(text.splitlines()) <= situation.text.BRIEF_MAX_LINES
    es = situation.brief(sit, "es")
    assert "Patrimonio neto 290,000" in es and "excedente ?" in es and "3.3 meses" in es
    assert "antes de ese pago quedan 40,000" in es
    assert "falta pago o plazo" in es and "sin asignar ?" in es
    en = situation.brief(sit, "en")
    assert "surplus ?" in en and "40,000 before it" in en and "tax residence not stated (lives in Mexico)" in en
    empty = WealthService(tmp_path / "e.sqlite3")
    empty.create("new", "New")
    assert situation.brief(empty.situation("new"), "en") == "Situation: nothing saved yet (first conversation)."


def test_memory_sentences_are_natural_and_point_at_their_facts(tmp_path):
    sit = _client(tmp_path, CANONICAL).situation("ana")
    en = {s["text"]: s for s in situation.sentences(sit, "en")}
    es = {s["text"]: s for s in situation.sentences(sit, "es")}
    assert "You take home about $85,000 MXN a month." in en
    assert "Your car loan: $60,000 left at 13% a year." in en
    assert "You want to invest $10,000 a month in S&P 500." in en
    assert "Recibes unos $85,000 MXN al mes, netos." in es
    assert "Te quedan $60,000 del crédito del coche, al 13% anual." in es
    assert "Quieres invertir $10,000 al mes en el S&P 500." in es
    car = es["Te quedan $60,000 del crédito del coche, al 13% anual."]
    assert [car["text"][a:b] for a, b in car["emphasis"]] == ["$60,000", "13%"]
    assert car["key"] == "liability.car" and car["source"] == "user" and car["age_days"] == 0 and not car["stale"]


def test_profile_shows_net_worth_names_and_the_model_missing_list(tmp_path):
    view = profile_view(_client(tmp_path, LIVE), "ana")
    json.dumps(view)
    assert view["overview"]["net_worth"] == 290000 and view["overview"]["status"] == "ready"
    labels = [e["label"] for g in view["groups"] for e in g["entries"]]
    assert "Invertir aproximadamente MXN 10,000 al mes en el S&P 500" in labels
    assert "Nu" in labels and "GBM" in labels and "Car loan" in labels and "Take-home pay" in labels
    assert not any(raw in label for label in labels for raw in ("sp500_monthly_investing", "debt0", "cash0", "_"))
    assert {"spending", "savings", "investments", "debts", "income", "goals"} <= set(view["completeness"]["known"])
    spending = next(g for g in view["groups"] if g["id"] == "money_out")["entries"]
    assert spending[0]["value"] == {"amount": 45000, "currency": "MXN", "period": "month"}
    assert view["memory"]["language"] == "en" and view["memory"]["es"]


def test_canonical_rows_edit_through_the_profile(tmp_path):
    from wealth.profile import fact_action
    service = _client(tmp_path, CANONICAL)
    facts = fact_action(service.inspect("ana"), "cash.nu", "edit", value={"amount": 160000, "currency": "MXN"})
    assert facts[0]["merge"] and facts[0]["value"] == {"amount": 160000, "currency": "MXN"}
    service.remember("ana", facts)
    assert service.situation("ana")["cash"][0]["amount"] == 160000


def test_schema_errors_name_the_field_and_the_fix(tmp_path):
    service = _client(tmp_path, {})
    cases = [
        ("liability.card", {"kind": "card", "balance": 1000, "currency": "MXN", "annual_rate": 45}, "write 0.45"),
        ("income.salary", {"amount": 1, "currency": "pesos", "frequency": "monthly"}, "income.salary.currency"),
        ("goals", [{"id": "casa", "target_amount": 1, "currency": "MXN"}], "goals[0].name is required"),
        ("cash.nu", {"amount": 0 - 5, "currency": "MXN"}, "at least 0"),
        ("client.profile", {"birth_year": 38}, "not an age"),
        ("onboarding", {"steps": {"name": "maybe"}, "started_at": "2026-09-21T10:00:00Z"}, "done|skipped|pending"),
    ]
    for key, value, message in cases:
        with pytest.raises(ValidationError, match=message.replace("[", r"\[").replace("]", r"\]").replace("|", r"\|")):
            service.remember("ana", [{"key": key, "value": value, "source": _source()}])
    receipt = service.remember("ana", [{"key": "plan.resources", "value": {"cash": [{"amount": 1}]}, "source": _source()}])
    assert "legacy shape" in receipt["warnings"][0]


def test_spending_comes_from_the_ledger_with_two_full_months(tmp_path):
    snapshot = {"client": {"revision": 1}, "facts": [
        {"key": "spending.monthly", "value": {"total": 99999, "currency": "MXN"}, "id": "s",
         "confidence": "reported", "source": _source(), "expires_on": None},
        {"key": "client.profile", "value": {"residence": {"country": "MX"}}, "id": "p",
         "confidence": "reported", "source": _source(), "expires_on": None}]}
    sit = situation.build(snapshot, cashflow_ledger(), date(2026, 9, 21))
    assert sit["spending"]["source"] == "ledger" and sit["spending"]["ledger_months"] == 2
    assert sit["spending"]["stated"]["total"] == 99999 and sit["spending"]["total"] != 99999


def test_threads_round_trip_into_the_brief(tmp_path):
    service = _client(tmp_path, LIVE)
    service.remember("ana", [{"key": "thread.car-first", "source": _source(), "value": {
        "kind": "advice", "text": "Pagar primero el crédito del auto al 13% antes de invertir", "status": "open",
        "related": ["plan.resources"]}}])
    sit = service.situation("ana")
    assert [t["id"] for t in sit["threads"]["open"]] == ["car-first"]
    assert sit["threads"]["open"][0]["created"] == TODAY.isoformat()
    assert "Pendiente [consejo" in situation.brief(sit, "es") and "crédito del auto al 13%" in situation.brief(sit, "es")
    service.remember("ana", [{"key": "thread.car-first", "merge": True, "source": _source(),
                              "value": {"status": "resolved", "resolution": "Liquidado en marzo"}}])
    sit = service.situation("ana")
    assert sit["threads"]["open"] == [] and sit["threads"]["closed"][0]["resolution"] == "Liquidado en marzo"
    assert "Pendiente" not in situation.brief(sit, "es")


def test_prompt_carries_the_brief_and_changes(tmp_path):
    service = _client(tmp_path, LIVE)
    brief, revision = agent.situation_brief(service.db_path, "ana", "¿Cómo voy con mi dinero? Quiero invertir")
    assert brief.startswith("Situación al") and revision == 1
    prompt = agent.build_prompt("hola", "ana", brief=brief)
    assert "<situation>" in prompt and "Patrimonio neto 290,000" in prompt and "Saved profile:" not in prompt
    service.remember("ana", [{"key": "reserve", "value": {"target_months": 6}, "source": _source()}])
    later, _ = agent.situation_brief(service.db_path, "ana", "ok", since_revision=revision)
    assert "Changed since r1: reserve" in later and "target 6 m" in later


def test_statement_insights_and_picture_after_confirm(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    service = _client(tmp_path, LIVE)
    folder = upload_dir("ana", service.db_path)
    folder.mkdir(parents=True)
    (folder / "gbm.pdf").write_bytes(fixtures.gbm_multicurrency())
    proposal = service.ingest("ana", "file", {"path": "gbm.pdf"})
    insights = {i["kind"]: i for i in proposal["result"]["insights"]}
    stated = insights["stated_vs_statement"]
    assert stated["stated"] == {"amount": 200000, "currency": "MXN"}
    assert stated["statement"] == {"MXN": 217837.35, "USD": 1000}
    assert stated["statement_value"] == 236087.35 and stated["difference"] == 36087.35
    assert insights["overlap"]["underlying"] == "S&P 500" and insights["overlap"]["symbols"] == ["CSPX", "IVV"]
    assert insights["concentration"]["symbol"] == "Udibono 2035" and insights["concentration"]["share"] > 0.39
    assert "IVV" in insights["domicile"]["us_domiciled"] and insights["domicile"]["ucits"] == ["CSPX"]
    assert insights["cash_drag"]["currency"] == "USD"

    saved = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    after = saved["result"]["picture_after"]
    assert after["net_worth_before"] == 290000 and after["net_worth_after"] == 326087.35
    assert after["change"] == 36087.35
    assert {c["kind"] for c in after["changed"]} == {"account_added", "stated_replaced_by_statement"}
    sit = service.situation("ana")
    assert sit["differences"][0]["statement"] == {"MXN": 217837.35, "USD": 1000}
    assert not next(i for i in sit["investments"] if i["institution"] == "GBM")["counted"]
    view = profile_view(service, "ana")
    assert view["overview"]["differences"][0]["institution"] == "GBM"
    labels = [e["label"] for g in view["groups"] for e in g["entries"]]
    assert "GBM (MXN)" in labels and not any("gbm-4" in label for label in labels)
    assert not any("gbm-4" in item["label"] for item in view["upcoming"])


def test_ingest_confirm_emits_a_memory_event():
    parser = agent._TurnParser()
    events = parser.feed(json.dumps({"type": "item.completed", "item": {
        "type": "mcp_tool_call", "server": "wealth", "tool": "wealth_ingest", "status": "completed",
        "arguments": {"client_id": "ana", "action": "confirm", "inputs": {"proposal_id": "p"}},
        "result": {"structured_content": {"status": "saved", "result": {"saved": {"keys": ["account.gbm-4567"]}}}}}}))
    assert [(e.type, dict(e.data)) for e in events] == [("memory", {"keys": ["account.gbm-4567"]})]


def test_plan_and_debt_payoff_read_the_model(tmp_path):
    facts = {**CANONICAL, "reserve": {"target_months": 3},
             "liability.car": {**CANONICAL["liability.car"], "payment": 3000, "payment_frequency": "monthly"},
             "goals": [*CANONICAL["goals"], {"id": "house", "name": "Enganche", "target_amount": 100000,
                                              "currency": "MXN", "target_date": "2028-06-01", "protect_now": True}]}
    service = _client(tmp_path, facts)
    plan = service.run("plan", client_id="ana")
    assert plan["status"] == "ready", plan["missing"]
    assert plan["result"]["available_capital"] == {"amount": "350000", "currency": "MXN"}
    assert plan["result"]["reserve_target"]["amount"] == "135000"
    assert plan["evidence_ids"] and any("monthly contribution" in a for a in plan["assumptions"])
    payoff = service.run("debt_payoff", {"monthly_amount": 10000}, client_id="ana")
    assert payoff["status"] == "ready" and payoff["result"]["avalanche"]["payoff"][0]["id"] == "car"
    assert payoff["evidence_ids"]
    calendar = service.run("calendar", client_id="ana")
    assert calendar["status"] == "ready" and len(calendar["result"]["months"]) == 12
    legacy = _client(tmp_path / "legacy", LIVE).run("plan", client_id="ana")
    assert legacy["status"] == "needs_input" and legacy["missing"][0]["key"] == "reserve.target_months"


def test_onboarding_missing_list_follows_step_order(tmp_path):
    service = _client(tmp_path, {**CANONICAL, "client.profile": {
        "name": "Ana", "birth_year": 1990, "residence": {"country": "MX", "region": "CDMX"}, "tax_residence": ["MX"],
        "dependents": 0, "language": "es"}, "preference.risk": {"drop_reaction": "hold", "experience": "some"},
        "onboarding": {"steps": {"name": "done"}, "started_at": "2026-09-21T10:00:00Z"}})
    assert situation.missing_for_onboarding(service.situation("ana")) == []
    fresh = _client(tmp_path / "f", {"onboarding": {"steps": {"birth_year": "skipped"}, "started_at": "2026-09-21T10:00:00Z"}})
    assert situation.missing_for_onboarding(fresh.situation("ana"))[:4] == ["name", "language", "residence", "tax_residence"]
    assert "birth_year" not in situation.missing_for_onboarding(fresh.situation("ana"))
    context = service.context("ana", intent="situation")
    assert context["brief"].startswith("Situación") and context["missing_for_onboarding"] == []


def test_older_flat_shapes_feed_income_retirement_assets_and_debt_rates(tmp_path):
    older = {
        "income.schedule": {"currency": "MXN", "monthly_take_home": 22000, "stability": "salaried"},
        "household": {"currency": "MXN", "complete": False, "as_of": TODAY.isoformat(), "people": [{"id": "p1"}],
                      "accounts": [], "positions": [], "lots": [], "fx": [], "fund_holdings": [], "income_exposures": [],
                      "external_assets": [{"id": "afore", "value": 310000, "currency": "MXN", "name": "AFORE", "liquid": False}],
                      "liabilities": [{"id": "tarjeta", "currency": "MXN", "annual_rate_percent": 72, "value": 68000}]},
    }
    service = _client(tmp_path, older)
    sit = service.situation("ana")
    assert sit["cash_flow"]["income"] == 22000
    assert sit["net_worth"]["illiquid"] == 310000 and sit["net_worth"]["liquid"] == 0
    assert sit["liabilities"][0]["annual_rate"] == 0.72


def test_statement_insight_names_holdings_that_changed_since_saved():
    before = {"holdings": {"saved": [{"ticker": "VOO", "symbol": "VOO (SIC)", "quantity": 7, "value": 80000,
                                      "institution": "GBM"}]}, "investments": [], "cash": []}
    result = {"household": {"currency": "MXN", "accounts": [{"id": "g", "institution": "GBM", "currency": "MXN"}],
                            "positions": [{"account_id": "g", "symbol": "SIC:VOO", "quantity": 5, "value": 54250,
                                           "currency": "MXN"}]}}
    change = next(i for i in situation.statement_insights(result, before) if i["kind"] == "position_change")
    assert change["symbol"] == "VOO" and change["saved"]["quantity"] == 7 and change["statement"]["quantity"] == 5
    assert "fewer" in change["text"]


def test_unknown_balances_are_never_zero_and_never_folded_into_a_statement(tmp_path):
    service = _client(tmp_path, {
        "income.salary": {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True},
        "spending.monthly": {"total": 45000, "currency": "MXN"},
        "cash.nu": {"currency": "MXN", "balance_unknown": True, "name": "Nu / banco", "liquid": True},
        "investment.afore": {"currency": "MXN", "balance_unknown": True, "name": "AFORE", "kind": "afore"},
        "investment.brokerage": {"amount": 200000, "currency": "MXN", "name": "GBM", "kind": "brokerage"},
    })
    sit = service.situation("ana")
    assert sit["net_worth"]["total"] is None and set(sit["net_worth"]["unknown_balances"]) >= {"Nu / banco", "AFORE"}
    assert sit["reserve"]["months"] is None  # liquid money of unknown size: the reserve is unknown, not empty
    brief = situation.brief(sit, "es")
    assert "no son cero" in brief and "Patrimonio neto 0" not in brief
    texts = [s["text"] for s in situation.sentences(sit, "es")]
    assert any("aún no sé" in t for t in texts)
    # A statement arrives: the AFORE (no institution, unknown balance) is not treated as covered by it.
    path = upload_dir("ana", service.db_path) / "gbm.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fixtures.gbm_multicurrency())
    proposal = service.ingest("ana", "file", {"path": "gbm.pdf"})
    service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"], "acknowledge_discrepancies": True})
    after = service.situation("ana")
    afore = next(r for r in after["investments"] if r["key"] == "investment.afore")
    assert afore["counted"] and afore["balance_unknown"]
    situation.brief(after, "es")  # renders without error


def test_confirming_a_statement_that_showed_the_difference_settles_it(tmp_path):
    service = _client(tmp_path, {
        "investment.gbm": {"amount": 200000, "currency": "MXN", "institution": "GBM", "kind": "brokerage",
                           "approximate": True},
    })
    path = upload_dir("ana", service.db_path) / "gbm.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fixtures.gbm_multicurrency())
    proposal = service.ingest("ana", "file", {"path": "gbm.pdf"})
    assert any(i["kind"] == "stated_vs_statement" for i in proposal["result"]["insights"])
    done = service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"],
                                             "acknowledge_discrepancies": True, "settle_differences": True})
    assert not done["result"]["needs_user"]
    assert service.contradictions("ana")["contradictions"] == []
    assert not service.situation("ana")["differences"]
