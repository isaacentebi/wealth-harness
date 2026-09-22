"""Findings from the third live run (Isaac in Monterrey, Mariana in CDMX; adv3-live).

Each test rebuilds the saved facts that went wrong and checks the picture, the brief, the You page, the
nudges, the memory prompt and the setup reveal now read them right.  No model is called.
"""
from __future__ import annotations

from datetime import date

import pytest

from wealth import agent, debt, proactive, situation, web
from wealth import onboarding as ob
from wealth.profile import _flow
from wealth.situation.plans import debt_payoff
from wealth.situation.schema import SchemaError, validate

TODAY = "2026-09-22"
MX = {"name": "Isaac", "residence": {"country": "MX", "city": "Monterrey"}}


def _snap(facts):
    rows = [{"id": f"f{i}", "key": k, "value": v, "status": "active", "confidence": "reported", "revision": i,
             "source": {"kind": "user", "ref": "chat", "observed_on": TODAY}}
            for i, (k, v) in enumerate(facts.items(), 1)]
    return {"client": {"id": "t", "revision": len(rows)}, "facts": rows, "decisions": []}


def _sit(facts):
    return situation.build(_snap(facts), None, TODAY)


CARD = {"kind": "card", "balance": 30000, "currency": "MXN", "annual_rate": 0.45, "payment": 2500,
        "payment_frequency": "monthly", "in_spending": False, "approximate": True}
# Isaac's facts as the live run left them (t.sqlite3), with rent saved as the memory step now writes it.
ISAAC = {
    "client.profile": MX,
    "income.principal": {"amount": 90000, "currency": "MXN", "frequency": "monthly"},
    "spending.monthly": {"essential": 12000, "partial": True, "components": ["renta"], "currency": "MXN"},
    "goals": [{"id": "enganche", "name": "Enganche", "currency": "MXN", "funded_amount": 50000}],
    "cash.nu": {"amount": 50000, "currency": "MXN", "institution": "Nu", "liquid": True, "name": "Nu"},
    "investment.cetes": {"amount": 120000, "currency": "MXN", "institution": "Cetesdirecto", "kind": "fund",
                         "name": "CETES"},
    "liability.card": CARD,
}


# -- 1, 2. the memory prompt saves a stated rate and a stated age ------------------------------------------


def test_memory_prompt_saves_a_stated_rate_and_an_age():
    text = " ".join(agent.memory_instructions().read_text(encoding="utf-8").split())
    assert "annual_rate: 0.45" in text and "approximate: true" in text  # the card example, rate included
    assert "a percentage on a debt or investment is its annual_rate" in text
    assert "birth_year_approximate: true" in text and "tengo 34 años" in text
    assert "partial: true" in text and 'components: ["renta"]' in text
    # The schema the step reads names the new fields.
    assert "birth_year_approximate?" in text and "partial?" in text and "components?" in text
    validate("liability.tarjeta", {"kind": "card", "balance": 30000, "currency": "MXN", "annual_rate": 0.45,
                                   "approximate": True})
    validate("investment.cetes", {"amount": 120000, "currency": "MXN", "annual_rate": 0.11})
    validate("cash.nu", {"amount": 50000, "currency": "MXN", "annual_rate": 0.15})
    validate("client.profile", {"birth_year": 1992, "birth_year_approximate": True})
    validate("spending.monthly", {"essential": 12000, "partial": True, "components": ["renta"], "currency": "MXN"})
    with pytest.raises(SchemaError):
        validate("investment.cetes", {"amount": 1, "currency": "MXN", "annual_rate": 11})  # 11 means 11%: 0.11
    with pytest.raises(SchemaError):
        validate("spending.monthly", {"essential": 12000, "components": "renta", "currency": "MXN"})


# -- 3. partial spending leaves the surplus, savings rate and reserve months unknown ------------------------


def test_rent_alone_is_not_the_month_nor_the_reserve_basis():
    sit = _sit(ISAAC)
    spending, flow, reserve = sit["spending"], sit["cash_flow"], sit["reserve"]
    assert spending["partial"] and spending["components"] == ["renta"] and spending["known_part"] == 12000
    assert spending["monthly"] is None and spending["essential_for_reserve"] is None
    assert flow["surplus"] is None and flow["spending"] is None
    assert reserve["months"] is None  # was 14.2 months of "essential" spending
    assert _flow(sit)["savings_rate"] is None and _flow(sit)["status"] == "unknown"  # was 81%
    unknown = next(u for u in sit["unknowns"] if u["code"] == "spending")
    assert unknown["partial"] == ["renta"] and unknown["known_part"] == 12000
    found = proactive.evaluate(sit, None, _snap(ISAAC), TODAY)
    assert "cash_drag" not in {i["kind"] for i in found["candidates"]}  # was "$26,000 more cash than..."
    brief = situation.brief(sit, "es")
    assert "gasto ?" in brief and "solo renta 12,000" in brief and "pregunta el gasto total" in brief
    assert "meses de reserva no se saben" in brief
    assert ob.picture(sit, "es")["reserve_months"] is None and ob.picture(sit, "es")["surplus"] is None


def test_essentials_without_a_total_leave_the_surplus_unknown_but_size_the_reserve():
    sit = _sit({**ISAAC, "spending.monthly": {"essential": 30000, "currency": "MXN"}, "goals": []})
    assert sit["cash_flow"]["surplus"] is None and _flow(sit)["savings_rate"] is None
    assert sit["reserve"]["months"] == pytest.approx(5.7, abs=0.05)  # 170,000 / 30,000 of complete essentials
    assert "total desconocido: solo esenciales 30,000" in situation.brief(sit, "es")
    # A total later is the whole month: everything is known again.
    both = _sit({**ISAAC, "spending.monthly": {"essential": 12000, "partial": True, "components": ["renta"],
                                               "total": 40000, "currency": "MXN"}})
    assert both["spending"]["monthly"] == 40000 and both["spending"]["essential_for_reserve"] == 40000
    assert both["cash_flow"]["surplus"] == 90000 - 40000 - 2500
    assert _flow(both)["savings_rate"] == pytest.approx((90000 - 40000 - 2500) / 90000, abs=1e-4)


def test_money_set_aside_for_the_down_payment_is_not_reserve():
    facts = {**ISAAC, "spending.monthly": {"total": 40000, "currency": "MXN"}}
    reserve = _sit(facts)["reserve"]
    # 50,000 Nu + 120,000 CETES, less the 50,000 apartados para el enganche.
    assert reserve["amount"] == 120000 and reserve["excluded_for_goals"] == 50000
    assert reserve["excluded_goals"] == ["enganche"] and reserve["months"] == 3.0
    assert sum(p["value"] for p in reserve["parts"]) == reserve["amount"]  # cash goes first
    assert [p["kind"] for p in reserve["parts"]] == ["instrument"]
    assert "(sin 50,000 apartados para Enganche)" in situation.brief(_sit(facts), "es")
    assert ob.picture(_sit(facts), "es")["reserve_amount"] == 120000
    # When the goal names its account, that account is already out of the reserve: nothing is taken twice.
    named = {**facts, "goals": [{"id": "enganche", "name": "Enganche", "currency": "MXN", "funded_amount": 50000,
                                 "accounts": ["cash.nu"]}]}
    assert _sit(named)["reserve"]["amount"] == 120000 and _sit(named)["reserve"]["excluded_for_goals"] is None
    # A designated reserve is the reserve they chose: goal money is not subtracted from it.
    designated = {**facts, "cash.nu": {**facts["cash.nu"], "purpose": "reserve"}}
    assert _sit(designated)["reserve"]["amount"] == 50000


# -- 4. one payoff engine: the debt engine's, IVA included -----------------------------------------------


def test_situation_payoff_matches_the_debt_engine_with_iva():
    sit = _sit(ISAAC)
    card = sit["liabilities"][0]
    assert card["payoff"]["months"] == 18 and card["payoff"]["date"] == "2028-03"
    assert card["payoff"]["interest"] == pytest.approx(13350.12, abs=0.01)  # was 17 months, 10,607
    assert card["iva_on_interest"] == 0.16
    engine = debt.run({"mode": "amortize"}, [{"id": "card", **CARD}], date(2026, 9, 22))["result"]["debts"][0]
    assert engine["months"] == card["payoff"]["months"] and engine["payoff_date"] == card["payoff"]["date"]
    assert engine["interest_cost"] == card["payoff"]["interest"]
    assert "intereses + IVA 13,350" in situation.brief(sit, "es")
    # A mortgage pays no IVA on interest; a US card neither.
    home = _sit({**ISAAC, "liability.card": {**CARD, "kind": "mortgage"}})["liabilities"][0]
    assert home["iva_on_interest"] == 0 and "iva" not in home["payoff"]
    # The You page's balance line ends at the same month.
    from wealth.profile import _debts
    row = _debts(sit)[0]
    assert row["points"][-1] == [18, 0.0] and row["months"] == 18


def test_debt_payoff_plans_charge_iva_like_the_engine():
    rows = [{"id": "card", **CARD, "monthly_payment": 2500},
            {"id": "personal", "kind": "personal", "balance": 80000, "currency": "MXN", "annual_rate": 0.22,
             "monthly_payment": 3000}]
    ours = debt_payoff(rows, 10000, as_of=TODAY)["result"]["avalanche"]
    engine = debt.run({"mode": "strategies", "monthly_amount": 10000}, rows, date(2026, 9, 22))["result"]
    avalanche = next(r for r in engine["strategies"] if r["strategy"] == "avalanche")
    assert ours["months"] == avalanche["months"]
    assert ours["interest"] == pytest.approx(avalanche["interest"], abs=1)


def test_a_term_derived_payment_clears_the_debt_in_that_term():
    loan = {"kind": "auto", "balance": 60000, "currency": "MXN", "annual_rate": 0.13, "remaining_term_months": 24}
    row = _sit({**ISAAC, "liability.card": loan})["liabilities"][0]
    assert row["payment_basis"] == "from remaining term" and row["payoff"]["months"] == 24
    engine = debt.run({"mode": "amortize"}, [{"id": "car", **loan}], date(2026, 9, 22))["result"]["debts"][0]
    assert engine["months"] == 24


# -- 5. debt questions run the debt task; a 0% transfer is computed with a stated assumption --------------


def test_instructions_route_every_debt_question_to_the_debt_task():
    text = " ".join(agent.conversation_instructions().read_text(encoding="utf-8").split())
    assert "Debt questions always run the debt task" in text
    for mode in ("amortize", "prepay_vs_invest", "refinance", "strategies"):
        assert f"({mode}" in text or f"mode {mode}" in text
    assert "Never draw a markdown table for a debt comparison" in text
    assert "assumes the card's current rate" in text


def test_a_zero_percent_transfer_without_a_later_rate_assumes_the_current_one():
    out = debt.run({"mode": "refinance", "offer": {"kind": "balance_transfer", "promo_rate": 0, "promo_months": 12,
                                                   "fee_percent": 0.03}},
                   [{"id": "tarjeta", **CARD}], date(2026, 9, 22))
    assert out["status"] == "ready" and out["result"]["fees"] == 900
    assert out["result"]["risk"]["go_to_rate"] == 0.45
    assert any("assumed to be your current rate (45% a year)" in a for a in out["assumptions"])
    with pytest.raises(ValueError):  # without a promo there is nothing to assume from
        debt.run({"mode": "refinance", "offer": {"kind": "refinance", "fee": 100}}, [{"id": "t", **CARD}],
                 date(2026, 9, 22))


# -- 6. the "I can now answer" nudge never shows raw thread text -------------------------------------------


def test_thread_ready_names_the_topic_not_the_thread_text():
    facts = [("client.profile", {**MX, "language": "es"}),
             ("thread.transferencia_saldo", {"kind": "advice", "status": "open",
                                             "text": "El 2026-09-22 dijo que le ofrecen transferir saldo al 0%",
                                             "related": ["liability.card"]}),
             ("liability.card", CARD)]
    snap = _snap(dict(facts))
    sit = situation.build(snap, None, TODAY)
    item = next(i for i in proactive.evaluate(sit, None, snap, TODAY)["candidates"] if i["kind"] == "thread_ready")
    assert item["title"] == {"en": "I can now update my advice on transferencia saldo",
                             "es": "Ya puedo actualizar mi consejo sobre transferencia saldo"}
    assert "El 2026" not in str(item["title"]) + str(item["why"]) + str(item["next_step"])
    assert item["why"]["es"] == "Llegaron datos nuevos: tu deuda." and item["why"]["en"] == "New details arrived: your debt."
    assert item["data"]["text"].startswith("El 2026-09-22")  # the chat still gets the note
    # A newer thread is not an input the older one was waiting for.
    only_threads = dict([("client.profile", MX),
                         ("thread.a", {"kind": "advice", "status": "open", "text": "x", "related": ["thread.b"]}),
                         ("thread.b", {"kind": "advice", "status": "open", "text": "y"})])
    snap = _snap(only_threads)
    found = proactive.evaluate(situation.build(snap, None, TODAY), None, snap, TODAY)["candidates"]
    assert "thread_ready" not in {i["kind"] for i in found}


# -- 7. the setup reveal speaks the conversation's language ------------------------------------------------


def test_reveal_language_follows_the_conversation():
    spanish = ["Me llamo Isaac, vivo en Monterrey y tengo 34 años",
               "gano 90 mil, pago 12 mil de renta, y tengo 50 mil apartados para el enganche"]
    assert ob.reveal_language({"profile": {}}, spanish, "en") == "es"
    assert ob.reveal_language({"profile": {"language": "en"}}, spanish, "en") == "es"
    assert ob.reveal_language({"profile": {"language": "en"}}, [], "es") == "en"  # set up in English, no chat
    assert ob.reveal_language({"profile": {}}, ["hola"], "en") == "en"  # too little to tell: the page's
    english = ["I make about 7,500 a month and spend most of it on rent and food"]
    assert ob.reveal_language({"profile": {"language": "es"}}, english, "es") == "en"
    assert ob.reveal_language({"profile": {}}, [], None) == "es"


def test_the_reveal_turn_is_written_in_spanish_after_a_spanish_chat_on_an_english_page(tmp_path, monkeypatch):
    chat = web.Chat(tmp_path / "w.sqlite3", "t")
    chat.messages = [{"role": "user", "content": "Me llamo Isaac, vivo en Monterrey y tengo 34 años"},
                     {"role": "assistant", "content": "Mucho gusto, Isaac."},
                     {"role": "user", "content": "gano 90 mil, pago 12 mil de renta y tengo 50 mil para el enganche"}]
    requests = []

    class _Turn:
        def summary(self):
            return {"internal": True}

    monkeypatch.setattr(chat, "start", lambda request, **kw: requests.append(request) or _Turn())
    steps = [("identity", {"name": "Isaac", "country": "MX"}), ("about", {"birth_year": 1992, "dependents": "0"}),
             ("income", {"amount": {"amount": 90000, "currency": "MXN"}}), ("spending", {"amount": "40 mil"})]
    for step, answer in steps:
        chat.answer_onboarding({"step": step, "answer": answer, "lang": "en"})
    for step in ("money", "debts", "goals", "risk", "statements"):
        chat.answer_onboarding({"step": step, "skip": True, "lang": "en"})
    assert len(requests) == 1 and "Write it in Mexican Spanish" in requests[0]
