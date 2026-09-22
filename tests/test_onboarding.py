"""Deterministic onboarding: steps, writers, skip/unsure, resume, parsing and the web endpoints."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests._pages import page_text
from wealth import onboarding as ob
from wealth import situation, web
from wealth.service import WealthService
from wealth.situation.schema import validate


@pytest.fixture()
def service(tmp_path):
    svc = WealthService(tmp_path / "w.sqlite3")
    svc.create("ana", "ana")
    return svc


def _ids(card, name="items"):
    field = next(f for f in card["fields"] if f["name"] == name)
    return [o["id"] for o in field["options"]]


ANSWERS = {
    "MX": [
        ("identity", {"name": "Ana", "country": "MX"}),
        ("about", {"birth_year": 1990, "dependents": "2"}),
        ("income", {"amount": {"amount": 85000, "currency": "MXN"},
                    "extras": {"aguinaldo": {"amount": {"amount": 40000, "currency": "MXN"}}, "ptu": {}}}),
        ("spending", {"amount": "45 mil"}),
        ("money", {"items": {"bank": {"amount": 60000}, "cetes": {"amount": "20k"}, "brokerage": {"amount": 200000},
                             "afore": {"upload": True}, "ppr": {"amount": 30000},
                             "us_broker": {"amount": {"amount": 1000, "currency": "USD"}}}}),
        ("debts", {"items": {"auto": {"balance": 150000, "rate": 13, "payment": {"amount": 6000, "currency": "MXN"}},
                             "card": {"balance": "20,000"}}}),
        ("goals", {"goals": ["emergency_fund", "home"], "target_amount": 300000, "target_year": 2029}),
        ("risk", {"drop_reaction": "hold", "experience": "some"}),
    ],
    "US": [
        ("identity", {"name": "Sam", "country": "US", "region": "California"}),
        ("about", {"birth_year": 1985, "dependents": "0"}),
        ("income", {"amount": 7500, "extras": {"bonus": {"amount": 10000}}}),
        ("spending", {"amount": "$4,500"}),
        ("money", {"items": {"bank": {"amount": 12000}, "brokerage": {"upload": True}, "retirement": {"amount": 80000},
                             "hsa": {"amount": 4000}}}),
        ("debts", {"items": {"student": {"balance": 18000, "rate": "5.5%", "payment": 300}}}),
        ("goals", {"goals": ["retirement"]}),
        ("risk", {"drop_reaction": "buy_more"}),
    ],
}


# ------------------------------------------------------------------ steps and conditions


def test_first_card_is_identity_in_both_languages(service):
    sit = service.situation("ana")
    es, en = ob.next_step(sit, "es"), ob.next_step(sit, "en")
    assert es["step"] == en["step"] == "identity"
    assert es["prompt"] == "¿Cómo te llamo y dónde vives?"
    assert en["prompt"] == "What should I call you, and where do you live?"
    assert es["index"] == 1 and es["total"] == len(ob.STEPS)
    assert es["unsure"] is False and es["prefill"] is None


def test_chips_follow_the_country(service):
    ob.apply(service, "ana", "identity", {"name": "Ana", "country": "MX"}, language="es")
    sit = service.situation("ana")
    money = ob.card(sit, "money", "es")
    assert _ids(money) == ["bank", "cetes", "brokerage", "afore", "ppr", "us_broker", "nu", "none"]
    labels = [o["label"] for o in money["fields"][0]["options"]]
    assert "Banco" in labels and "Nu" in labels and "GBM / casa de bolsa" in labels and "Broker en EE. UU." in labels
    options = {o["id"]: o for o in money["fields"][0]["options"]}
    assert money["currency"] == "MXN" and options["us_broker"]["fields"][0]["currency"] == "USD"
    # Extra income is never prompted: only take-home pay is asked.
    assert [f["name"] for f in ob.card(sit, "income", "es")["fields"]] == ["amount"]
    assert "student" not in _ids(ob.card(sit, "debts", "es"))

    us = WealthService(service.db_path)
    us.create("sam", "sam")
    ob.apply(us, "sam", "identity", {"name": "Sam", "country": "US"}, language="en")
    sit = us.situation("sam")
    assert _ids(ob.card(sit, "money", "en")) == ["bank", "brokerage", "retirement", "hsa", "none"]
    assert [o["label"] for o in ob.card(sit, "money", "en")["fields"][0]["options"]][2] == "401(k) / IRA"
    assert [f["name"] for f in ob.card(sit, "income", "en")["fields"]] == ["amount"]
    assert "student" in _ids(ob.card(sit, "debts", "en"))
    assert ob.card(sit, "spending", "en")["currency"] == "USD"
    # The state field only shows for the United States.
    region = next(f for f in ob.card(sit, "identity", "en")["fields"] if f["name"] == "region")
    assert region["when"] == {"country": "US"}


@pytest.mark.parametrize("country", ["MX", "US"])
def test_every_writer_produces_valid_canonical_facts(service, country):
    sit = service.situation("ana")
    for step, answer in ANSWERS[country]:
        facts, status, summary, _ = ob.build_facts(sit, step, answer, language="es" if country == "MX" else "en",
                                                   today="2026-09-21")
        assert status == "done" and summary
        for fact in facts:
            assert fact["source"] == {"kind": "user", "ref": "onboarding", "observed_on": "2026-09-21"}
            assert fact["confidence"] == "confirmed" and fact["merge"] is True
            if fact["key"] != "onboarding":  # a merge patch; the store validates the merged value
                assert validate(fact["key"], fact["value"]) == []
        service.remember("ana", facts)
        sit = service.situation("ana")
    keys = {f["key"] for f in service.inspect("ana")["facts"]}
    assert {"client.profile", "income.salary", "spending.monthly", "goals", "preference.risk", "onboarding"} <= keys
    if country == "MX":
        assert {"income.aguinaldo", "cash.bank", "investment.cetes", "investment.brokerage", "investment.ppr",
                "investment.us_broker", "liability.auto", "liability.card"} <= keys
        assert "income.ptu" not in keys  # extra income chosen without an amount stays unknown
        afore = next(f for f in service.inspect("ana")["facts"] if f["key"] == "investment.afore")
        assert afore["value"]["balance_unknown"] is True and "amount" not in afore["value"]  # held, amount unknown
        assert sit["net_worth"]["total"] is None and "AFORE" in " ".join(sit["net_worth"]["unknown_balances"])
        assert sit["income"]["monthly"] == 85000 and sit["spending"]["monthly"] == 45000
        auto = next(r for r in sit["liabilities"] if r["id"] == "auto")
        assert auto["annual_rate"] == 0.13 and auto["monthly_payment"] == 6000 and auto["payoff"]["status"] == "ready"
        home = next(g for g in sit["goals"] if g["id"] == "home-down-payment")
        assert home["target_amount"] == 300000 and home["target_date"] == "2029-12-31"
        assert sit["profile"]["language"] == "es" and sit["profile"]["dependents"] == 2
    else:
        assert sit["profile"]["residence"] == {"country": "US", "region": "California", "city": None}
        brokerage = next(f for f in service.inspect("ana")["facts"] if f["key"] == "investment.brokerage")
        assert brokerage["value"]["balance_unknown"] is True
        student = next(r for r in sit["liabilities"] if r["id"] == "student")
        assert student["annual_rate"] == 0.055 and student["currency"] == "USD"
    assert ob.next_step(sit)["step"] == "statements"


def test_apply_returns_next_card_and_running_picture(service):
    for step, answer in ANSWERS["MX"][:4]:
        result = ob.apply(service, "ana", step, answer, language="es")
    assert result["answered"] == {"step": "spending", "status": "done", "summary": "Gastas $45,000 al mes"}
    assert result["card"]["step"] == "money"
    assert result["picture"]["surplus"] == 40000
    assert result["picture"]["line"] == "Te quedan $40,000 al mes"
    result = ob.apply(service, "ana", "money", {"items": {"bank": {"amount": 90000}}}, language="en")
    assert result["picture"]["net_worth"] == 90000 and result["picture"]["reserve_months"] == 2
    assert result["picture"]["line"] == "Net worth $90,000 · $40,000 left each month · 2 months of reserve"


def test_invalid_answers_are_refused_before_anything_is_written(service):
    for step, answer in [("identity", {"name": "", "country": "MX"}), ("about", {"birth_year": 1850}),
                         ("income", {"amount": "mucho"}), ("money", {"items": {"none": {}, "bank": {}}}),
                         ("debts", {"items": {"card": {"balance": 100, "rate": 250}}}),
                         ("goals", {"goals": ["a", "b", "c"]}), ("risk", {"drop_reaction": "panic"}),
                         ("nope", {"x": 1})]:
        with pytest.raises(ValueError):
            ob.apply(service, "ana", step, answer)
    assert service.inspect("ana")["facts"] == []


# ------------------------------------------------------------------ skip, not sure, resume


def test_skip_and_not_sure_are_settled_and_never_asked_again(service):
    ob.apply(service, "ana", "identity", {"name": "Ana", "country": "MX"}, language="es")
    ob.skip(service, "ana", "about", language="es")
    # "Not sure" and "Skip" are one action on the card: a typed "no sé" records the step as skipped.
    result = ob.apply(service, "ana", "income", {"unsure": True}, language="es")
    assert result["answered"]["status"] == "skipped" and result["answered"]["summary"] == "Ingreso: omitido"
    assert result["card"]["step"] == "spending" and result["card"]["unsure"] is False and result["card"]["skip"] is True
    sit = service.situation("ana")
    steps = sit["profile"]["onboarding"]["steps"]
    assert steps["birth_year"] == steps["dependents"] == "skipped" and steps["income"] == "skipped"
    assert sit["income"]["monthly"] is None  # unknown stays unknown, never zero
    assert not any(f["key"].startswith("income.") for f in service.inspect("ana")["facts"])
    progress = ob.progress(sit)
    assert progress["skipped"] == ["about", "income"] and progress["unsure"] == []
    assert ob.parse_free_text(ob.card(sit, "spending", "es"), "no sé") == {"status": "parsed", "answer": {"unsure": True}}
    with pytest.raises(ValueError):
        ob.apply(service, "ana", "identity", {"unsure": True})


def test_resume_prefills_what_is_known_and_confirms_in_one_tap(service):
    ob.apply(service, "ana", "identity", {"name": "Ana", "country": "MX"}, language="es")
    # Learned elsewhere (a conversation): spending exists before its card is reached.
    service.remember("ana", [{"key": "spending.monthly", "value": {"total": 45000, "currency": "MXN"},
                              "source": {"kind": "user", "ref": "chat", "observed_on": "2026-09-21"},
                              "confidence": "confirmed"}])
    ob.skip(service, "ana", "about")
    ob.apply(service, "ana", "income", {"amount": 85000})
    card = ob.next_step(service.situation("ana"), "es")
    assert card["step"] == "spending" and card["confirm"] is True
    assert card["prefill"] == {"amount": {"amount": 45000, "currency": "MXN"}}
    result = ob.apply(service, "ana", "spending", {"confirm": True}, language="es")
    assert result["answered"]["summary"] == "Gastas $45,000 al mes" and result["card"]["step"] == "money"
    history = [f for f in service.inspect("ana")["facts"] if f["key"] == "spending.monthly"]
    assert history[0]["revision"] == 2  # confirming writes the step status only, not the value again
    # A new session picks up at the first pending card; editing a done card reopens it prefilled.
    assert ob.next_step(service.situation("ana"))["step"] == "money"
    edit = ob.card(service.situation("ana"), "income", "es")
    assert edit["status"] == "done" and edit["prefill"]["amount"] == {"amount": 85000, "currency": "MXN"}
    ob.apply(service, "ana", "income", {"amount": 90000}, language="es")
    assert service.situation("ana")["income"]["monthly"] == 90000


def test_completion_happens_once_and_the_brief_lists_progress(service):
    for step, answer in ANSWERS["MX"]:
        assert ob.apply(service, "ana", step, answer, language="es")["completed_now"] is False
    done = ob.skip(service, "ana", "statements", language="es")
    assert done["complete"] and done["completed_now"] and done["card"] is None
    again = ob.apply(service, "ana", "risk", {"drop_reaction": "sell"}, language="es")
    assert again["completed_now"] is False and again["card"] is None
    brief = situation.brief(service.situation("ana"), "es")
    line = next(l for l in brief.splitlines() if l.startswith("Onboarding"))
    assert line.startswith("Onboarding complete; done: identity, about, income, spending, money, debts, goals, risk")
    assert "not asked again" in line


def test_brief_shows_progress_while_in_progress(service):
    assert "Onboarding" not in situation.brief(service.situation("ana"), "en")
    ob.apply(service, "ana", "identity", {"name": "Ana", "country": "MX"})
    ob.apply(service, "ana", "spending", {"unsure": True})
    ob.skip(service, "ana", "debts")
    line = next(l for l in situation.brief(service.situation("ana"), "en").splitlines() if l.startswith("Onboarding"))
    assert "in progress" in line and "done: identity" in line and "skipped: spending, debts" in line
    assert "pending: about, income, money, goals, risk" in line


# ------------------------------------------------------------------ typed answers


@pytest.mark.parametrize("text, amount, currency", [
    ("85 mil", 85000, None), ("85k", 85000, None), ("85K", 85000, None), ("$85,000", 85000, None),
    ("85,000 pesos", 85000, "MXN"), ("85.000", 85000, None), ("gano como 85 mil al mes", 85000, None),
    ("1.5 millones", 1500000, None), ("2 millones de pesos", 2000000, "MXN"), ("US$1,000", 1000, "USD"),
    ("1,000 dólares", 1000, "USD"), ("about 4.5k", 4500, None), ("$4,500.50 a month", 4500.5, None),
    ("3 thousand dollars", 3000, "USD"), ("85000 MXN", 85000, "MXN"),
])
def test_amount_parsing_in_spanish_and_english(text, amount, currency):
    assert ob.parse_amount(text) == {"amount": amount, "currency": currency}


@pytest.mark.parametrize("text", ["", "mucho", "entre 40 y 50 mil", "no sé", "20 o 30 mil"])
def test_amounts_that_need_the_model(text):
    assert ob.parse_amount(text) is None


def test_free_text_answers_the_card_or_hands_off_to_the_model(service):
    ob.apply(service, "ana", "identity", {"name": "Ana", "country": "MX"}, language="es")
    sit = service.situation("ana")
    income = ob.card(sit, "income", "es")
    assert ob.parse_free_text(income, "como 85 mil") == {
        "status": "parsed", "answer": {"amount": {"amount": 85000, "currency": "MXN"}}}
    assert ob.parse_free_text(income, "1,000 dólares")["answer"]["amount"]["currency"] == "USD"
    assert ob.parse_free_text(income, "no sé") == {"status": "parsed", "answer": {"unsure": True}}
    assert ob.parse_free_text(income, "¿cuenta el aguinaldo?")["status"] == "needs_model"
    assert ob.parse_free_text(income, "What counts as take-home") == {"status": "needs_model", "reason": "question"}
    assert ob.parse_free_text(income, "depende del mes, entre 40 y 60")["status"] == "needs_model"
    about = ob.card(sit, "about", "es")
    assert ob.parse_free_text(about, "1990, dos hijos")["answer"] == {"birth_year": 1990, "dependents": "2"}
    assert ob.parse_free_text(about, "nací en 1988")["answer"] == {"birth_year": 1988}
    risk = ob.card(sit, "risk", "en")
    assert ob.parse_free_text(risk, "hold")["answer"] == {"drop_reaction": "hold"}
    debts = ob.card(sit, "debts", "es")
    assert ob.parse_free_text(debts, "No")["answer"] == {"items": {"none": {}}}
    identity = ob.card(sit, "identity", "es")
    assert ob.parse_free_text(identity, "Soy Ana y vivo en Monterrey")["status"] == "needs_model"


# ------------------------------------------------------------------ web endpoints


@contextmanager
def serving(chat):
    server = web.create_server(chat, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _get(base, path, token=None, headers=None):
    return urlopen(Request(base + path, headers={**({"X-Wealth-Token": token} if token else {}), **(headers or {})}),
                   timeout=10)


def _post(base, path, token, body, headers=None):
    return urlopen(Request(base + path, data=json.dumps(body).encode(), method="POST",
                           headers={"Content-Type": "application/json", "X-Wealth-Token": token, **(headers or {})}),
                   timeout=10)


def test_onboarding_endpoints_need_the_token_and_a_local_host(tmp_path):
    chat = web.Chat(tmp_path / "w.sqlite3", "ana")
    with serving(chat) as (base, _):
        for call in (lambda: _get(base, "/api/onboarding"), lambda: _get(base, "/api/onboarding", "wrong"),
                     lambda: _post(base, "/api/onboarding", "wrong", {"step": "identity", "skip": True}),
                     lambda: _get(base, "/api/onboarding", chat.token, {"Host": "evil.example"}),
                     lambda: _post(base, "/api/onboarding", chat.token, {"step": "identity", "skip": True},
                                   {"Origin": "https://evil.example"})):
            with pytest.raises(HTTPError) as denied:
                call()
            assert denied.value.code == 403
        assert WealthService(chat.db).inspect("ana")["facts"] == []
        card = json.load(_get(base, "/api/onboarding?lang=es", chat.token))["card"]
        assert card["step"] == "identity" and card["language"] == "es"
        for body in ({"step": "nope", "skip": True}, {"step": "identity", "answer": {"name": ""}},
                     {"step": "identity", "lang": "fr", "skip": True}):
            with pytest.raises(HTTPError) as bad:
                _post(base, "/api/onboarding", chat.token, body)
            assert bad.value.code == 400 and json.load(bad.value)["kind"] == "invalid"


def test_flow_to_completion_triggers_exactly_one_reveal_turn(tmp_path, monkeypatch):
    calls = []

    def turn(message, **kwargs):
        calls.append((message, kwargs))
        return "Tienes un patrimonio neto de $110,000. Siguiente paso: arma tu reserva."

    monkeypatch.setattr(web, "run_turn", turn)
    chat = web.Chat(tmp_path / "w.sqlite3", "ana")
    with serving(chat) as (base, _):
        state = json.load(_get(base, "/api/state?lang=es"))
        assert state["onboarding"]["active"] and state["onboarding"]["mode"] == "flow"
        assert state["onboarding"]["card"]["prompt"] == "¿Cómo te llamo y dónde vives?" and state["starters"] == []
        token = state["csrf_token"]
        for step, answer in ANSWERS["MX"]:
            if step == "spending":
                typed = json.load(_post(base, "/api/onboarding", token, {"step": step, "text": "como 45 mil", "lang": "es"}))
                assert typed["answered"]["summary"] == "Gastas $45,000 al mes"
                continue
            result = json.load(_post(base, "/api/onboarding", token, {"step": step, "answer": answer, "lang": "es"}))
            assert result["reveal"] is None and result["card"]["step"] != step
        question = json.load(_post(base, "/api/onboarding", token,
                                   {"step": "statements", "text": "¿qué es un estado de cuenta?", "lang": "es"}))
        assert question == {"needs_model": True, "reason": "question"}
        assert not calls
        done = json.load(_post(base, "/api/onboarding", token, {"step": "statements", "skip": True, "lang": "es"}))
        assert done["complete"] and done["card"] is None and done["reveal"]["internal"] is True
        # The AFORE was chosen without a balance, so net worth is unknown rather than computed as if it were 0.
        assert done["reveal"]["message"] == "" and done["picture"]["net_worth"] is None
        chat.turn.wait(10)
        # Answering again after completion never starts another reveal.
        again = json.load(_post(base, "/api/onboarding", token, {"step": "risk", "answer": {"drop_reaction": "sell"}}))
        assert again["reveal"] is None
        after = json.load(_get(base, "/api/state?lang=es"))
    assert len(calls) == 1
    message, kwargs = calls[0]
    assert message.startswith("Setup just finished") and "Mexican Spanish" in message
    assert "one short paragraph" in message and "single next step" in message
    assert "Onboarding complete" in kwargs["brief"] and "Patrimonio neto" in kwargs["brief"]
    assert kwargs["history"] == []
    # The reveal shows as the first assistant message; Wealth's own request never shows as the person's words.
    assert [m["role"] for m in after["messages"]] == ["assistant"]
    assert after["messages"][0]["content"].startswith("Tienes un patrimonio neto")
    assert after["onboarding"] == {"active": False, "card": None}
    assert after["starters"] == []


def test_returning_profile_gets_a_single_continue_row(tmp_path):
    db = tmp_path / "w.sqlite3"
    service = WealthService(db)
    service.create("ana", "ana")
    ob.apply(service, "ana", "identity", {"name": "Ana", "country": "MX"}, language="es")
    chat = web.Chat(db, "ana")
    state = chat.state("es")
    assert state["onboarding"]["mode"] == "resume" and state["onboarding"]["card"]["step"] == "about"
    assert state["onboarding"]["remaining"] == 8
    # Once the person answers in this session, a reload keeps the cards inline.
    chat.answer_onboarding({"step": "about", "skip": True, "lang": "es"})
    assert chat.state("es")["onboarding"]["mode"] == "flow"


def test_page_renders_cards_safely_and_in_both_languages():
    page = page_text("chat")
    assert "'/api/onboarding'" in page and "Continuar configuración" in page and "Continue setup" in page
    assert "No estoy seguro" in page and "Omitir" in page and "Subir un estado de cuenta" in page
    assert "aria-pressed" in page and "min-height: 44px" in page
    for sink in ("innerHTML =", "innerHTML=", "outerHTML", "insertAdjacentHTML"):
        assert sink not in page
