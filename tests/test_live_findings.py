"""Findings from a live adversarial run on the real model (Mariana in CDMX, Sam in Austin).

Each test rebuilds the saved facts that went wrong and checks the picture, the setup card, the memory
prompt and the nudges now read them right.  No model is called.
"""
from __future__ import annotations

import pytest

from wealth import agent, proactive, situation
from wealth import onboarding as ob
from wealth.service import WealthService
from wealth.situation.schema import SchemaError, validate

TODAY = "2026-09-21"
MX = {"name": "Mariana", "language": "es", "residence": {"country": "MX", "city": "Ciudad de México"}}
US = {"name": "Sam", "residence": {"country": "US", "region": "Texas", "city": "Austin"}}


def _snap(facts):
    rows = [{"id": f"f{i}", "key": k, "value": v, "status": "active", "confidence": "reported", "revision": i,
             "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-21"}}
            for i, (k, v) in enumerate(facts.items(), 1)]
    return {"client": {"id": "c", "revision": len(rows)}, "facts": rows, "decisions": []}


def _sit(facts):
    return situation.build(_snap(facts), None, TODAY)


# Mariana's saved facts, as the live run left them (adv.sqlite3).
MARIANA = {
    "client.profile": MX,
    "income.salary": {"amount": 70000, "currency": "MXN", "frequency": "monthly", "net": True, "approximate": True},
    "spending.monthly": {"total": 52000, "currency": "MXN", "approximate": True},
    "cash.nu": {"amount": 60000, "currency": "MXN", "institution": "Nu", "liquid": True, "name": "Nu"},
    "investment.afore": {"amount": 310000, "currency": "MXN", "kind": "afore", "name": "AFORE"},
    "investment.brokerage": {"amount": 220000, "currency": "MXN", "institution": "GBM", "kind": "brokerage",
                             "name": "GBM"},
    "investment.cetes": {"amount": 180000, "currency": "MXN", "institution": "Cetesdirecto", "kind": "fund",
                         "name": "CETES"},
    "liability.auto": {"kind": "auto", "balance": 240000, "currency": "MXN", "annual_rate": 0.145, "payment": 7900,
                       "payment_frequency": "monthly"},
    "goals": [{"id": "home-down-payment", "name": "Home down payment", "action": "buy", "currency": "MXN",
               "target_amount": 1200000, "target_date": "2029-12-31", "priority": "high"}],
}


# -- 1. the reserve counts CETES and other cash-like instruments ------------------------------------------


def test_reserve_counts_cetes_and_names_its_parts():
    sit = _sit(MARIANA)
    reserve = sit["reserve"]
    assert reserve["amount"] == 240000 and reserve["months"] == 4.6  # 60k Nu + 180k CETES over 52k a month
    assert reserve["source_keys"] == ["cash.nu", "investment.cetes"]  # never GBM equities or the AFORE
    assert [(p["kind"], p["label"], p["value"]) for p in reserve["parts"]] == [
        ("cash", None, 60000), ("instrument", "CETES", 180000)]
    brief = situation.brief(sit, "es")
    assert "Reserva 60,000 efectivo + 180,000 CETES = 240,000 = 4.6 meses" in brief


@pytest.mark.parametrize("item, counted", [
    ({"kind": "fund", "name": "Fondo de mercado de dinero"}, True),
    ({"kind": "other", "institution": "Klar", "name": "Inversión Klar"}, True),
    ({"kind": "fund", "name": "Sofipo a la vista"}, True),
    ({"kind": "other", "name": "Pagaré", "liquidity_days": 28}, True),
    ({"kind": "other", "name": "Pagaré a un año", "liquidity_days": 365}, False),
    ({"kind": "brokerage", "name": "CETES en GBM"}, False),        # a brokerage account is never the reserve
    ({"kind": "retirement", "name": "PPR de ahorro"}, False),
    ({"kind": "fund", "name": "Fondo de acciones"}, False),
])
def test_what_counts_as_cash_like(item, counted):
    facts = {**MARIANA, "investment.cetes": {**item, "amount": 100000, "currency": "MXN"}}
    assert (_sit(facts)["reserve"]["amount"] == 160000) is counted


def test_cetes_earmarked_for_a_goal_stay_out_of_the_reserve_and_fund_the_goal():
    facts = {**MARIANA, "investment.cetes": {**MARIANA["investment.cetes"], "purpose": "goal:home-down-payment"}}
    sit = _sit(facts)
    assert sit["reserve"]["amount"] == 60000
    goal = sit["goals"][0]
    assert goal["funded"] == 180000 and goal["funded_basis"] == "accounts"
    listed = {**MARIANA, "goals": [{**MARIANA["goals"][0], "accounts": ["investment.cetes"]}]}
    assert _sit(listed)["reserve"]["amount"] == 60000
    reserve_cetes = {**MARIANA, "investment.cetes": {**MARIANA["investment.cetes"], "purpose": "reserve"}}
    assert _sit(reserve_cetes)["reserve"]["basis"] == "designated"
    assert _sit(reserve_cetes)["reserve"]["amount"] == 180000


# -- 2. the picture card's "liquid" agrees with the reserve beside it --------------------------------------


def test_picture_liquid_is_the_reserve_money():
    sit = _sit(MARIANA)
    picture = ob.picture(sit, "es")
    assert picture["liquid"] == 240000 and picture["liquid_basis"] == "reserve"
    assert picture["liquid"] == picture["reserve_amount"] and picture["reserve_months"] == 4.6
    assert picture["liquid_total"] == 460000  # GBM included: that is the net-worth split, not "liquid" on the card
    assert [p["label"] for p in picture["reserve_parts"]] == [None, "CETES"]


# -- 3, 5, 8. the memory prompt ----------------------------------------------------------------------------


def _memory_prompt() -> str:
    return " ".join(agent.memory_instructions().read_text(encoding="utf-8").split())


def test_memory_prompt_keeps_impulses_out_of_preferences():
    text = _memory_prompt()
    assert "A request, fear or impulse in the moment is never a preference.* fact" in text
    assert '"quiero vender todo ya" during a fall' in text
    assert "dated thread" in text and "Never write it as preference.*" in text
    assert "never change preference.risk from one reaction" in text
    assert "Preferences are stable and explicit" in text


def test_memory_prompt_puts_payments_on_the_debt_and_set_asides_on_the_goal():
    text = _memory_prompt()
    assert "A debt payment belongs on its debt: liability.<id> {payment, payment_frequency}" in text
    assert "never constraint.*" in text and "save in_spending true or false" in text
    assert '"tengo 400k apartados para el enganche") is the goal\'s funded_amount' in text
    assert "liquidity_days" in text


def test_the_schema_refuses_a_payment_saved_as_a_constraint():
    with pytest.raises(SchemaError, match="liability.<id>"):
        validate("constraint.mortgage_payment", {"payment": 3100, "currency": "USD"})
    with pytest.raises(SchemaError, match="debt payment"):
        validate("constraint.pago_hipoteca", {"text": "Paga 3,100 al mes"})
    validate("constraint.leverage", {"text": "No borrowing to invest"})  # a real constraint is fine
    validate("goals", [{"id": "casa", "name": "Casa", "currency": "MXN", "target_amount": 1200000,
                        "funded_amount": 400000, "accounts": ["investment.cetes", "cash.nu"]}])
    with pytest.raises(SchemaError, match="currency"):
        validate("goals", [{"id": "casa", "name": "Casa", "funded_amount": 400000}])
    with pytest.raises(SchemaError, match="accounts"):
        validate("goals", [{"id": "casa", "name": "Casa", "accounts": ["goals"]}])
    validate("investment.cetes", {"amount": 1, "currency": "MXN", "liquidity_days": 28, "purpose": "reserve"})


def test_a_stated_set_aside_is_the_goal_progress():
    goals = [{**MARIANA["goals"][0], "funded_amount": 400000}]
    goal = _sit({**MARIANA, "goals": goals})["goals"][0]
    assert goal["funded"] == 400000 and goal["funded_basis"] == "stated" and goal["target_date"] == "2029-12-31"
    assert _sit(MARIANA)["goals"][0]["funded"] is None  # nothing said: unknown, never 0


# -- 5. a legacy constraint.*_payment is read as the debt payment --------------------------------------------


SAM = {
    "client.profile": US,
    "income.take_home": {"amount": 14000, "currency": "USD", "frequency": "monthly", "net": True},
    "spending.monthly": {"total": 9500, "currency": "USD"},
    "cash.schwab_checking": {"amount": 38000, "currency": "USD", "institution": "Schwab"},
    "liability.mortgage": {"kind": "mortgage", "balance": 410000, "currency": "USD", "annual_rate": 0.064},
    "constraint.mortgage_payment": {"currency": "USD", "liability": "liability.mortgage", "payment": 3100,
                                    "note": "Payment frequency not explicitly stated."},
}


def test_legacy_constraint_payment_is_the_mortgage_payment():
    sit = _sit(SAM)
    mortgage = sit["liabilities"][0]
    assert mortgage["monthly_payment"] == 3100 and mortgage["missing"] == []
    assert mortgage["payment_from"] == "constraint.mortgage_payment" and mortgage["payment_frequency_assumed"]
    assert "constraint.mortgage_payment" in sit["evidence"]
    flow = sit["cash_flow"]
    assert flow["debt_payments"] == 3100 and flow["debt_payments_unknown"] == []
    # Whether the 3,100 is inside the 9,500 was never said: asked, with both readings.
    assert flow["surplus"] is None and flow["surplus_range"] == {"low": 1400, "high": 4500}
    told = {**SAM, "liability.mortgage": {**SAM["liability.mortgage"], "in_spending": False}}
    assert _sit(told)["cash_flow"]["surplus"] == 1400
    inside = {**SAM, "liability.mortgage": {**SAM["liability.mortgage"], "in_spending": True}}
    assert _sit(inside)["cash_flow"]["surplus"] == 4500


def test_a_payment_on_the_liability_wins_over_the_constraint():
    facts = {**SAM, "liability.mortgage": {**SAM["liability.mortgage"], "payment": 3000, "payment_frequency": "monthly"}}
    assert _sit(facts)["liabilities"][0]["monthly_payment"] == 3000


# -- 6. the debts card asks whether the payment is already inside spending -----------------------------------


@pytest.fixture()
def service(tmp_path):
    svc = WealthService(tmp_path / "w.sqlite3")
    svc.create("m", "m")
    return svc


def test_debts_card_asks_if_the_payment_is_inside_spending(service):
    ob.apply(service, "m", "identity", {"name": "Mariana", "country": "MX"}, language="es")
    ob.apply(service, "m", "spending", {"amount": {"amount": 52000, "currency": "MXN"}}, language="es")
    card = ob.card(service.situation("m"), "debts", "es")
    auto = next(o for o in card["fields"][0]["options"] if o["id"] == "auto")
    question = next(f for f in auto["fields"] if f["name"] == "in_spending")
    assert question["label"] == "¿Este pago ya está dentro de tus $52,000 de gasto?"
    assert [o["id"] for o in question["options"]] == ["yes", "no"] and question["required"] is False
    ob.apply(service, "m", "income", {"amount": {"amount": 70000, "currency": "MXN"}}, language="es")

    def answer(inside):
        detail = {"balance": {"amount": 240000, "currency": "MXN"}, "rate": 14.5,
                  "payment": {"amount": 7900, "currency": "MXN"}}
        if inside is not None:
            detail["in_spending"] = inside
        return ob.apply(service, "m", "debts", {"items": {"auto": detail}}, language="es")

    result = answer(None)  # not answered: unknown, never subtracted silently
    assert service.situation("m")["liabilities"][0]["in_spending"] is None
    assert result["picture"]["surplus"] is None
    assert result["picture"]["surplus_range"] == {"low": 10100, "high": 18000}
    assert "entre $10,100 y $18,000 al mes" in result["picture"]["line"]
    assert answer("yes")["picture"]["surplus"] == 18000
    assert answer("no")["picture"]["surplus"] == 10100
    assert answer("Sí, ya está incluido")["picture"]["surplus"] == 18000  # a typed answer reads the same
    assert ob.card(service.situation("m"), "debts", "es")["prefill"]["items"]["auto"]["in_spending"] == "yes"


# -- 4. the setup card clears on the free-text path ----------------------------------------------------------


def test_setup_card_follows_the_facts_told_in_chat():
    facts = {**SAM, "investment.401k": {"amount": 610000, "currency": "USD", "kind": "retirement"},
             "goals": [{"id": "retirement", "name": "Both retire around 62", "action": "retire"}],
             "preference.risk": {"drop_reaction": "sell"}}
    sit = _sit(facts)
    card = ob.next_step(sit, "en")
    assert card["step"] == "about"  # never "What should I call you" again: name and city are known
    assert ob.remaining(sit) == ["about", "statements"]  # the statements offer comes after the questions
    told = _sit({**facts, "client.profile": {**US, "birth_year": 1981, "dependents": 2}})
    assert ob.next_step(told, "en") is None and ob.remaining(told) == []


def test_starting_the_cards_after_chat_keeps_what_is_known(service):
    service.remember("m", [{"key": k, "value": v, "source": {"kind": "user", "ref": "chat", "observed_on": TODAY}}
                           for k, v in (("client.profile", MX), ("spending.monthly", MARIANA["spending.monthly"]))])
    assert ob.next_step(service.situation("m"))["step"] == "about"
    result = ob.apply(service, "m", "about", {"birth_year": 1995, "dependents": "0"}, language="es")
    assert result["card"]["step"] == "income"  # identity and spending stay answered once the cards start
    assert set(ob.progress(service.situation("m"))["done"]) >= {"identity", "about", "spending"}


def test_in_the_flow_a_known_step_is_still_confirmed_once(service):
    ob.apply(service, "m", "identity", {"name": "Mariana", "country": "MX"}, language="es")
    service.remember("m", [{"key": "spending.monthly", "value": {"total": 45000, "currency": "MXN"},
                            "source": {"kind": "user", "ref": "chat", "observed_on": TODAY}}])
    ob.skip(service, "m", "about")
    ob.apply(service, "m", "income", {"amount": 85000})
    assert ob.next_step(service.situation("m"))["confirm"] is True


# -- 7. no prepay-the-debt nudge while the reserve comes first ------------------------------------------------


def _nudges(facts):
    snap = _snap(facts)
    sit = situation.build(snap, None, TODAY)
    return {i["kind"]: i for i in proactive.evaluate(sit, None, snap, TODAY)["candidates"]}


_FLOW = {"client.profile": MX,
         "income.salary": {"amount": 80500, "currency": "MXN", "frequency": "monthly"},
         "spending.monthly": {"total": 45000, "currency": "MXN"},
         "liability.auto": {"kind": "auto", "balance": 180000, "currency": "MXN", "annual_rate": 0.14,
                            "payment": 10000, "payment_frequency": "monthly", "in_spending": False}}
_CAR = {"kind": "advice", "status": "open", "related": ["liability.auto"],
        "text": "Manda los $25,500 restantes al mes al crédito del auto hasta liquidarlo"}


def test_advice_to_fill_the_reserve_is_never_read_as_paying_the_car():
    reserve = {"kind": "advice", "status": "open", "related": ["spending.monthly", "liability.auto"],
               "text": "Destina este mes los 25,500 de excedente a la reserva, sin abono extra al coche"}
    item = _nudges({**_FLOW, "thread.reserva": reserve})["follow_through"]
    assert item["title"]["es"] == "¿Ya mandas los $25,500 a tu reserva?"
    assert "al auto" not in item["title"]["es"]


def test_prepay_nudge_waits_while_the_reserve_is_below_target_or_advised():
    assert _nudges({**_FLOW, "thread.auto": _CAR})["follow_through"]["title"]["es"].endswith("al auto?")
    low = {**_FLOW, "thread.auto": _CAR, "cash.nu": {"amount": 20000, "currency": "MXN"},
           "reserve": {"target_months": 3}}
    assert "follow_through" not in _nudges(low)
    other = {"kind": "advice", "status": "open", "text": "Primero llena tu fondo de emergencia a tres meses"}
    assert "follow_through" not in _nudges({**_FLOW, "thread.auto": _CAR, "thread.reserva": other})


def test_live_mariana_gets_no_car_prepay_nudge():
    thread = {"kind": "advice", "status": "open",
              "related": ["income.salary", "spending.monthly", "cash.nu", "liability.auto", "goals"],
              "text": "Recomendé destinar este mes los 10,100 MXN de excedente calculado a la reserva (de 60,000 a "
                      "70,100), mantener el pago del coche sin abono extra"}
    facts = {**MARIANA, "liability.auto": {**MARIANA["liability.auto"], "in_spending": False},
             "thread.reserva_este_mes": thread}
    found = _nudges(facts)
    assert "al auto" not in found.get("follow_through", {}).get("title", {}).get("es", "")


# -- idle cash: CETES fill the reserve first and are never called idle ------------------------------------------


def test_idle_yield_lets_cetes_fill_the_reserve_first():
    base = {"client.profile": {**MX, "tax_residence": ["MX"]},
            "spending.monthly": {"essential": 20000, "currency": "MXN"}, "reserve": {"target_months": 6},
            "cash.bbva": {"amount": 200000, "currency": "MXN", "institution": "BBVA"}}
    alone = _nudges(base)["idle_yield"]["data"]
    assert alone["idle"] == 80000  # 200,000 checking - 120,000 reserve
    with_cetes = _nudges({**base, "investment.cetes": {"amount": 100000, "currency": "MXN", "kind": "fund",
                                                        "name": "CETES"}})["idle_yield"]["data"]
    # CETES hold 100,000 of the 120,000 reserve: only 20,000 has to sit in checking; the CETES are not idle.
    assert with_cetes["idle"] == 180000 and with_cetes["kept"]["reserve_in_instruments"] == 100000


def test_a_goal_linked_to_a_replaced_estimate_counts_the_statement_balance():
    from datetime import date
    from decimal import Decimal
    from wealth.situation import model as m
    goal = {"id": "casa", "accounts": ["cash.gbm"], "currency": "MXN"}
    rows = [
        {"key": "cash.gbm", "amount": 400000, "currency": "MXN", "value": 400000, "counted": False,
         "covered_by": ["account.gbm-1234"]},
        {"key": "account.gbm-1234", "amount": 425000, "currency": "MXN", "value": 425000, "purpose": "goal"},
    ]
    m._goal_funding([goal], rows, m._FX(date(2026, 9, 21)), "MXN")
    assert Decimal(str(goal["funded"])) == Decimal("425000")
    assert goal["funded_sources"] == ["account.gbm-1234"]
